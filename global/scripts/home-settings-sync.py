#!/usr/bin/env python3
'Ensure ~/.claude/settings.json wires the store-managed global hooks and settings.\n\nThe store owns a fixed set of guardrail + materialization + autosync hooks, plus a\nshort list of settings values that must be identical fleet-wide (MANAGED_TOP for\ntop-level keys, MANAGED_ENV for env keys). settings.json is per-machine (it also\nholds machine-local entries, such as hooks installed by other tools on that machine)\nand is therefore not clobbered: we only ensure our managed hooks, top-level keys and\nenv keys are present, in canonical order, and never duplicated. Everything else is\npreserved.\n\nIdempotent and machine-portable ($HOME-relative). Optional argv[1] overrides the\ntarget file (used by tests). home-materialize projects hooks and scripts; this\nscript keeps the file that wires them the same on every machine.\nObservations guarded: #8.'
import json, os, re, sys
from collections import OrderedDict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_paths as hp

HOME = os.path.expanduser("~")
H = hp.hooks_dir(HOME)
S = os.path.join(HOME, ".agent-context", "global", "scripts")






CS = hp.scripts_dir(HOME)








def _load_agent_python():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-python.py")
    spec = importlib.util.spec_from_file_location("agent_python", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


AGENT_PYTHON = _load_agent_python()


def _load_hook_client_build():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-client-build.py")
    spec = importlib.util.spec_from_file_location("hook_client_build", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOK_CLIENT = _load_hook_client_build()


def command_basename(command):
    'Basename of the script a hook command runs, past any interpreter prefix.'
    words = (command or "").split()
    while words and AGENT_PYTHON.is_interpreter(words[0]):
        words = words[1:]
    return os.path.basename(words[0]) if words else ""


def wired_command(path):
    "A managed hook's command. A .py hook runs through this host's interpreter: a bare\n    shebang resolves `python3` on the caller's PATH, which is 3.8 on the Synology nodes."
    words = path.split()
    script = words[0] if words else path
    command = "%s %s" % (AGENT_PYTHON.interpreter(), path) if script.endswith(".py") else path
    return hp.command_path(command)


def _store_write_matcher():
    "Every MCP tool that can carry written text into the store, derived.\n\n    store-prose-tools builds the list from the server's own tool signatures and\n    exits 2 on a parameter it has never classified, so a hand-typed list cannot\n    fall short of the real writers.\n    Observations guarded: #317, #318.\n\n    Fails closed. If the derivation cannot run, fall back to the full literal set\n    rather than to a subset: a guard that over-fires is noise, a guard that\n    under-fires publishes a credential to four remotes within seconds."
    import subprocess
    fallback = "|".join("mcp__agent-context__" + t for t in (
        "add_audit_observation", "bulk_edit", "edit_body",
        "resolve_audit_observation",
        "update_audit_observation", "upsert_agent_definition", "upsert_command",
        "upsert_doc", "upsert_hook", "upsert_instruction", "upsert_memory",
        "upsert_script", "upsert_skill",
    ))
    try:
        out = subprocess.run(
            [sys.executable, os.path.join(S, "store-prose-tools.py"), "--matcher"],
            capture_output=True, text=True, timeout=20,
        )
        derived = out.stdout.strip()
        if out.returncode == 0 and derived.startswith("mcp__agent-context__"):
            return derived
    except Exception:
        pass
    return fallback


STORE_WRITE_TOOLS = _store_write_matcher()




MANAGED = [
    
    ("WorktreeCreate", None, [
        (os.path.join(S, "worktree-create.py"), {"timeout": 120}),
    ]),
    
    
    
    
    
    ("SessionStart", "compact", [
        (os.path.join(H, "compact-invalidates-bootstrap.py"), {"timeout": 10}),
    ]),
    ("SessionStart", None, [
        
        
        
        
        
        
        
        
        
        (os.path.join(S, "home-materialize.py") + " --session-start", {"timeout": 20}),
        
        
        
        
        
        
        
        
        
        
        (os.path.join(CS, "project-materialize.py"), {"timeout": 20}),
        (os.path.join(H, "audit-nudge.py"), {}),
        
        
        
        
        (os.path.join(H, "unlanded-work-check.py"), {"timeout": 15}),
        
        
        
        (os.path.join(H, "file-memory-drift-check.py"), {"timeout": 10}),
        (os.path.join(H, "remind-critical-rules.py"), {}),
        (os.path.join(H, "context-audit-autorun.py"), {"timeout": 15}),
        (os.path.join(H, "project-memory-verify-autorun.py"), {"timeout": 15}),
        
        
        
        
        
        
        
        
        
        (os.path.join(H, "invariant-probe.py"), {"timeout": 10}),
        (os.path.join(H, "chrome-mcp-isolated-guard.py"), {"timeout": 10}),
        
        
        
        (os.path.join(H, "ralph-patch-guard.py"), {"timeout": 10}),
        
        
        
        
        (os.path.join(H, "orphan-sweep.py"), {"timeout": 10}),
        
        
        
        (os.path.join(H, "token-usage-collect.py"), {"timeout": 10}),
        
        
        
        
        
        
        
        
        
        
        
        
        
        
        (os.path.join(H, "preflight-core-health.py"), {"timeout": 120}),
    ]),
    
    
    
    
    
    
    
    
    
    
    
    
    ("UserPromptSubmit", None, [
        
        
        
        (os.path.join(H, "codex-test-unlock.py"), {"timeout": 90}),
        (os.path.join(H, "require-store-bootstrap.py"), {"timeout": 10}),
        
        
        
    ]),
    
    
    
    
    
    
    
    ("PreToolUse", "*", [
        (os.path.join(H, "require-store-bootstrap.py"), {"timeout": 10}),
    ]),
    ("PreToolUse", "Bash", [
        
        
        
        
        (os.path.join(H, "block-secret-read.py"), {"timeout": 10}),
        (os.path.join(H, "block-git-stash.py"), {}),
        
        
        
        
        (os.path.join(H, "block-destructive-git.py"), {"timeout": 10}),
        
        
        
        
        
        (os.path.join(H, "block-agent-file-force-add.py"), {}),
        (os.path.join(H, "block-deploy.py"), {}),
        
        
        
        
        
        
        
        
        (os.path.join(H, "block-coauthor-trailer.py"), {"timeout": 10}),
        
        
        
        
        
        
        
        (os.path.join(H, "block-consent-self-grant.py"), {"timeout": 10}),
        (os.path.join(H, "guard-git-write.py"), {}),
        
        
        
        
        
        
        
        (os.path.join(H, "require-worktree-edit-bash.py"), {"timeout": 15}),
        
        
        
        
        
        
        
        (os.path.join(H, "require-worktree-add-location.py"), {"timeout": 15}),
        
        
        
        
        (os.path.join(H, "block-write-outside-home.py"), {"timeout": 15}),
        
        
        (os.path.join(H, "block-locked-test-edit.py"), {"timeout": 10}),
        
        
        
        
        
        
        
        (os.path.join(H, "block-sleep-poll.py"), {"timeout": 15}),
        
        
        
        
        
        
        
        
        (os.path.join(H, "block-unreaped-spawn.py"), {"timeout": 15}),
        
        
        
        
        
        
        
        (os.path.join(H, "block-blind-recursive-delete.py"), {"timeout": 15}),
        
        
        
        
        
        
        
        
        
        
        (os.path.join(H, "block-shell-file-read.py"), {"timeout": 10}),
        
        
        
        
        
        
        
        
        
        
        (os.path.join(H, "require-resolvable-read-path.py"), {"timeout": 10}),
        
        
        
        (os.path.join(H, "warn-long-foreground-command.py"), {"timeout": 10}),
    ]),
    
    
    
    ("PreToolUse", "Monitor", [
        (os.path.join(H, "block-write-outside-home.py"), {"timeout": 15}),
    ]),
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit", [
        
        (os.path.join(H, "block-file-memory-write.py"), {}),
        
        (os.path.join(H, "require-worktree-edit.py"), {}),
        
        
        (os.path.join(H, "block-write-outside-home.py"), {"timeout": 15}),
        
        
        (os.path.join(H, "block-locked-test-edit.py"), {"timeout": 10}),
        
        
        
        (os.path.join(H, "block-consent-self-grant.py"), {"timeout": 10}),
    ]),
    
    
    
    
    ("PreToolUse", "mcp__agent-context__run_store_task", [
        (os.path.join(H, "block-consent-self-grant.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    ("PreToolUse", "EnterWorktree|ExitWorktree", [
        (os.path.join(H, "block-worktree-move-mid-flight.py"), {"timeout": 15}),
    ]),
    
    
    
    
    
    
    
    
    ("PreToolUse",
     "Write|Edit|MultiEdit|NotebookEdit|Bash|" + STORE_WRITE_TOOLS, [
        (os.path.join(H, "plain-language-check.py"), {"timeout": 15}),
    ]),
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    ("PostToolUse", "mcp__[a-zA-Z0-9]+-lsp__.*|LSP", [
        (os.path.join(H, "lsp-failure-tripwire.py"), {"timeout": 10}),
    ]),
    
    
    
    
    ("PostToolUseFailure", "mcp__[a-zA-Z0-9]+-lsp__.*|LSP", [
        (os.path.join(H, "lsp-failure-tripwire.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit|Task|Agent|Workflow", [
        (os.path.join(H, "require-working-lsp.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    
    
    ("PreToolUse",
     "mcp__agent-context__add_audit_observation|"
     "mcp__agent-context__update_audit_observation|"
     "mcp__agent-context__resolve_audit_observation", [
        (os.path.join(H, "audit-observation-guard.py"), {"timeout": 15}),
    ]),
    
    
    
    
    
    
    
    ("PreToolUse", STORE_WRITE_TOOLS, [
        (os.path.join(H, "memory-husk-guard.py"), {"timeout": 15}),
    ]),
    
    
    
    
    
    
    ("PreToolUse",
     "mcp__agent-context__upsert_instruction|mcp__agent-context__edit_body", [
         (os.path.join(H, "block-instruction-budget-overrun.py"), {"timeout": 15}),
     ]),
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    ("PostToolUse", "Write|Edit|MultiEdit|mcp__agent-context__.*", [
        (os.path.join(H, "agents-remateralize.py"), {"timeout": 20}),
    ]),
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    
    ("PreToolUse", "AskUserQuestion", [
        (os.path.join(H, "approval-question.py"), {"timeout": 30}),
    ]),
    ("PostToolUse", "AskUserQuestion", [
        (os.path.join(H, "approval-question.py"), {"timeout": 90}),
    ]),
    ("PostToolUse", "Read", [
        (os.path.join(H, "read-width-nudge.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    
    
    ("PostToolUse", "Bash", [
        (os.path.join(H, "warn-truncated-search.py"), {"timeout": 10}),
        
        
        (os.path.join(H, "warn-long-foreground-command.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    
    
    ("PostToolUse", "Edit|Write", [
        (os.path.join(H, "post-edit-verify.py"), {"timeout": 30}),
        
        (os.path.join(H, "record-session-claim.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    ("PostToolUseFailure", "*", [
        (os.path.join(H, "failure-trace.py"), {"timeout": 10}),
    ]),
    
    
    ("UserPromptSubmit", None, [
        
        
        
        (os.path.join(H, "sync-fault-notice.py"), {"timeout": 10}),
        
        
        
        
        (os.path.join(H, "record-session-claim.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    
    
    
    
    
    
    ("PreCompact", None, [
        (os.path.join(H, "precompact-capture.py"), {"timeout": 15}),
    ]),
    ("UserPromptSubmit", None, [
        (os.path.join(H, "verification-claim-check.py"), {"timeout": 15}),
        
        
        
        
        
        (os.path.join(H, "terse-output-check.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    
    
    
    
    
    
    
    ("PreToolUse", "Read", [
        (os.path.join(H, "block-redundant-read.py"), {"timeout": 10}),
    ]),
    
    
    ("PreToolUse", "Edit|MultiEdit", [
        (os.path.join(H, "require-investigation-before-edit.py"), {"timeout": 10}),
    ]),
    
    
    ("PreToolUse", "Bash", [
        (os.path.join(H, "block-destructive-data-command.py"), {"timeout": 10}),
    ]),
    ("Stop", None, [
        
        
        
        
        
        
        
        (os.path.join(H, "require-structured-questions.py"), {"timeout": 10}),
        
        
        
        
        (os.path.join(H, "block-handing-user-a-runnable-step.py"), {"timeout": 10}),
        
        
        
        
        (os.path.join(H, "block-stop-with-open-work.py"), {"timeout": 10}),

        
        
        
        
        
        
        (os.path.join(H, "verification-claim-gate.py"), {"timeout": 15}),

        
        
        
        
        (os.path.join(H, "locked-test-drift-gate.py"), {"timeout": 15}),

        
        
        
        
        
        
        
        
        
        
        
        (os.path.join(H, "terse-output-gate.py"), {"timeout": 15}),

        
        
        
        (os.path.join(H, "memory-capture-on-correction.py"), {"timeout": 10}),

        
        
        
        
        
        
        (os.path.join(H, "sync-fault-notice.py"), {"timeout": 10}),

        
        
        
        
        
        (os.path.join(H, "token-usage-collect.py") + " --min-interval 30", {"timeout": 10}),
        
        
        
        
        
        
        (os.path.join(H, "agent-turn-finished-notify.py"), {"timeout": 10}),
        
        
        
        
        
        
        
        
        
        
        (os.path.join(H, "ralph-cleanup-stop.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    ("Notification", None, [
        (os.path.join(H, "agent-blocked-notify.py"), {"timeout": 10}),
    ]),
    
    
    
    
    ("SubagentStop", None, [
        (os.path.join(H, "subagent-return-gate.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    ("SessionEnd", None, [
        (os.path.join(H, "token-usage-collect.py"), {"timeout": 10}),
        
        (os.path.join(H, "record-session-claim.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    ("PostToolUse", "*", [(os.path.join(H, "peer-message-notice.py"), {"timeout": 10})]),
    
    
    
    
    
    ("PostToolUse", "*", [(os.path.join(H, "compress-tool-output.py"), {"timeout": 10})]),
    ("UserPromptSubmit", None, [(os.path.join(H, "peer-message-notice.py"), {"timeout": 10})]),
    
    
    ("Stop", None, [(os.path.join(H, "peer-message-notice.py"), {"timeout": 15})]),
    
    
    ("Stop", None, [(os.path.join(H, "record-session-claim.py"), {"timeout": 10})]),
    
    ("SessionEnd", None, [(os.path.join(H, "peer-message-notice.py"), {"timeout": 10})]),
    
    
    ("PreToolUse", "SendMessage|ListAgents", [
        (os.path.join(H, "redirect-native-peer-message.py"), {"timeout": 10}),
    ]),
    
    
    
    
    
    
    ("SessionStart", None, [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
    ("UserPromptSubmit", None, [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
    ("PreToolUse", "*", [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
    ("PostToolUse", "*", [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
    ("Notification", None, [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
    ("Stop", None, [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
    ("SessionEnd", None, [(os.path.join(H, "iterm-tab-status.py"), {"timeout": 5})]),
]




WINDOWS_SKIPPED = {
    "iterm-tab-status.py": "iTerm2 tab status is Mac-only, and no Windows terminal reads it",
}


def runs_here(path, windows=None):
    'False for a MANAGED hook this host leaves out (WINDOWS_SKIPPED on Windows).'
    windows = os.name == "nt" if windows is None else windows
    return not (windows and command_basename(path) in WINDOWS_SKIPPED)









import starter_profile
MANAGED = [(event, matcher, [(path, extra) for path, extra in commands if starter_profile.enabled(path)])
           for event, matcher, commands in MANAGED]

MANAGED_BASENAMES = {command_basename(p)
                     for _, _, cmds in MANAGED for p, _ in cmds if p.strip()}










DISPATCHED_EVENTS = ("PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
                     "Stop", "SessionStart", "SessionEnd")
DISPATCHER = os.path.join(CS, "hook-dispatch.py")
MANAGED_BASENAMES.add(os.path.basename(DISPATCHER))





LIVE_CLIENT = None







CLIENT_LIMIT = {"PreToolUse": 240}


def dispatch_command(event, client=None):
    'The settings.json command for a dispatched event: through `client`, the hook-client\n    binary that hands it to the warm hook server (policy), when one is given, else the\n    dispatcher itself. hook-client-build.py, which home-materialize runs first, builds it.'
    command = "%s %s" % (wired_command(DISPATCHER), event)
    return "%s %s" % (hp.command_path(client), command) if client else command


def _load_hook_registry():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hook-registry.py")
    spec = importlib.util.spec_from_file_location("hook_registry", path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load " + path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


HOOK_REGISTRY = _load_hook_registry()


def stays_direct(path, extra):
    "A hook the dispatcher must not absorb. An async hook's output reaches Claude on a\n    later turn, which a synchronous dispatcher cannot reproduce, and home-materialize\n    writes the ~/.claude projection the dispatcher itself runs from."
    return bool(extra.get("async")) or command_basename(path) == "home-materialize.py"


















































MANAGED_ENV = {
    
    
    
    
    
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5",
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "61",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "12",
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "2",
    "PYTHONUNBUFFERED": "1",
    
    
    
    
    "TMPDIR": os.path.join(HOME, ".cache", "tmp"),
    
    
    
    
    
    
    
    "CLAUDE_CODE_TMPDIR": os.path.join(HOME, ".cache", "claude-tmp"),
}














RETIRED_ENV = set()
RETIRED_TOP = set()
assert not (RETIRED_ENV & set(MANAGED_ENV)), \
    "a key cannot be both asserted and retired; the pop would undo the update"






























































MANAGED_TOP = {
    "crossSessionInbound": "accept",
    "autoCompactEnabled": True,
    "autoCompactWindow": 1000000,
    "subagentPromptCacheTtl": "1h",
    "autoMemoryEnabled": False,
    "outputStyle": "Concise",
    "preferredNotifChannel": "iterm2",
    "teammateMode": "tmux",
}
assert all(isinstance(v, (str, int, float, bool)) for v in MANAGED_TOP.values()), \
    "MANAGED_TOP is scalar-only; a container value would clobber machine-local entries"


assert not (RETIRED_TOP & set(MANAGED_TOP)), \
    "a top-level key cannot be both asserted and retired; the pop would undo the update"






































MANAGED_NESTED = {
    "worktree": {"baseRef": "head"},
    
    
    
    "statusLine": {"type": "command",
                   "command": wired_command(os.path.join(CS, "statusline-command.py"))},
    
    
    
    
    "attribution": {"commit": "", "pr": ""},
    "enabledPlugins": {
        "csharp-lsp@claude-plugins-official": False,
        "firebase@firebase": False,
    },
    "permissions": {
        "deny": [
            "Read(~/.ssh/id_*)",
            "Read(~/.ssh/*.pem)",
            "Read(~/.ssh/*.key)",
            "Read(~/.aws/credentials)",
            "Read(~/.git-credentials)",
            "Read(~/.docker/config.json)",
            "Read(~/.netrc)",
            "Read(~/.pgpass)",
            "Read(~/Library/Keychains/**)",
            
            
            
            
            
            
            
            "Edit(~/.ssh/**)",
            "Edit(~/.aws/**)",
            
            
            
            "SendFeedback",
            
            
            
            
            
            
            
            "Bash(sudo su*)",
            "Bash(sudo -i*)",
            "Bash(sudo -s*)",
            "Bash(sudo bash*)",
            "Bash(sudo sh *)",
            "Bash(sudo zsh*)",
            "Bash(sudo fish*)",
            "Bash(sudo python*)",
            "Bash(sudo perl*)",
            "Bash(sudo rm *)",
            "Bash(sudo dd *)",
            "Bash(sudo mkfs*)",
            "Bash(sudo chown *)",
            "Bash(sudo chmod *)",
            "Bash(sudo visudo*)",
            "Bash(sudo passwd*)",
            "Bash(sudo usermod*)",
            "Bash(sudo userdel*)",
            "Bash(sudo useradd*)",
            "Bash(sudo tee /etc/sudoers*)",
            "Bash(dd *)",
            "Bash(mkfs*)",
            "Bash(git push --force *)",
            "Bash(git push -f *)",
            "Bash(git reset --hard *)",
        ],
        
        
        
        
        
        
        
        
        
        "defaultMode": "bypassPermissions",
    },
}
assert all(
    not isinstance(v, dict) for leaves in MANAGED_NESTED.values() for v in leaves.values()
), "MANAGED_NESTED leaves must be scalars or lists; a dict leaf would drop sibling keys"


















MANAGED_ALLOW = [
    f"mcp__{server}-lsp__{tool}"
    for server in ("csharp", "swift", "kotlin", "typescript")
    for tool in ("definition", "hover", "references", "diagnostics")
]


def basename_of(entry):
    return command_basename(entry.get("command", ""))


def is_managed(entry):
    return basename_of(entry) in MANAGED_BASENAMES


def is_retired(entry):
    "True when the entry points into the store's global/hooks or global/scripts, or into\n    the retired Claude-side hooks and scripts projections, at a file that is gone.\n\n    A path under the store's directories with no file behind it is a hook the store\n    retired. Without this prune the dangling settings.json entry\n    survives on every other machine and fails on every session. Scoped to the store-owned dir, so machine-local hooks\n    living anywhere else are never touched. The two scripts dirs joined in Consolidation\n    Phase 4, which renamed the store's shell scripts to .py with no stubs, so the\n    shell-era materialize entries retire the same way."
    cmd = entry.get("command", "") or ""
    return any(
        not os.path.exists(m.group(0))
        for root in (H, CS, S, os.path.join(hp.claude_home(HOME), "hooks"),
                     os.path.join(hp.claude_home(HOME), "scripts"))
        for m in re.finditer(re.escape(root) + r"/[\w.-]+", cmd)
    )


def matcher_eq(group_matcher, want):
    if want is None:
        return not group_matcher  
    return group_matcher == want


def managed_entry(path, extra):
    entry = {"type": "command", "command": wired_command(path)}
    entry.update(extra)
    return entry


def dispatch_plan(event, specs, registry, existing=None):
    '[(matcher, entries)] for a dispatched event; its guards go into registry[event].\n\n    The registry keeps the event\'s groups as the undispatched sync would build them: a\n    group that still holds a machine-local hook keeps its place, the rest follow in\n    canonical order, and a repeated matcher merges by prepending, as step 2 below does.\n    Groups left empty by the strip do not anchor the order; if they did, a second sync\n    of a dispatched event would reorder the registry it wrote the first time.\n    A hook that stays direct is left in settings.json and holds its place in the\n    registry as {"direct": <script>}, so hook-registry.py rebuilds the original order.\n    The dispatcher joins the event\'s no-matcher group after the direct hooks, and its\n    timeout covers every guard it runs (600 s, Claude Code\'s default, for one with none),\n    or is CLIENT_LIMIT\'s value when hook-client bounds the event itself.'
    direct, budget = [], 0
    groups = [dict(g, hooks=[]) for g in existing or [] if g.get("hooks")]
    for matcher, cmds in specs:
        entries = []
        for path, extra in cmds:
            entry = managed_entry(path, extra)
            if stays_direct(path, extra):
                direct.append((matcher, entry))
                entries.append({"direct": command_basename(path)})
            else:
                entries.append(entry)
                budget += int(entry.get("timeout") or 600)
        target = next((g for g in groups if matcher_eq(g.get("matcher"), matcher)), None)
        if target is None:
            target = {} if matcher is None else {"matcher": matcher}
            target["hooks"] = []
            groups.append(target)
        target["hooks"] = entries + target["hooks"]
    registry[event] = [g for g in groups if g["hooks"]]
    plan = OrderedDict()
    for matcher, entry in direct:
        plan.setdefault(matcher, []).append(entry)
    plan.setdefault(None, []).append({
        "type": "command",
        "command": dispatch_command(event, LIVE_CLIENT),
        "timeout": min(budget, CLIENT_LIMIT.get(event, budget)) if LIVE_CLIENT else budget,
    })
    return list(plan.items())


def sync(settings, dispatched=None, registry=None):
    'Assert the managed values and hooks in `settings`.\n\n    `dispatched` names the events whose guards run behind hook-dispatch.py (default\n    DISPATCHED_EVENTS); their guards are written into `registry`, a dict of event ->\n    groups that main() saves as hook-dispatch.json.'
    dispatched = DISPATCHED_EVENTS if dispatched is None else tuple(dispatched)
    registry = {} if registry is None else registry
    settings.update(MANAGED_TOP)
    for key, leaves in MANAGED_NESTED.items():
        container = settings.get(key)
        if not isinstance(container, dict):
            container = {}
            settings[key] = container
        container.update(leaves)
    
    
    perms = settings.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    if isinstance(allow, list):
        have = set(allow)
        allow.extend(rule for rule in MANAGED_ALLOW if rule not in have)
    for key in RETIRED_TOP:
        settings.pop(key, None)
    env = settings.setdefault("env", {})
    env.update(MANAGED_ENV)
    for key in RETIRED_ENV:
        env.pop(key, None)
    hooks = settings.setdefault("hooks", {})
    
    
    for event, groups in hooks.items():
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not is_retired(h)]
    
    
    by_event = OrderedDict()
    for event, matcher, cmds in MANAGED:
        cmds = [(path, extra) for path, extra in cmds if runs_here(path)]
        if cmds:
            by_event.setdefault(event, []).append((matcher, cmds))
    for event, specs in by_event.items():
        groups = hooks.setdefault(event, [])
        
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", []) if not is_managed(h)]
        
        
        
        if event in dispatched:
            wanted = dispatch_plan(event, specs, registry, groups)
        else:
            wanted = [(matcher, [managed_entry(path, extra) for path, extra in cmds])
                      for matcher, cmds in specs]
        for matcher, managed_entries in wanted:
            target = next((g for g in groups if matcher_eq(g.get("matcher"), matcher)), None)
            if target is None:
                target = {} if matcher is None else {"matcher": matcher}
                target["hooks"] = []
                groups.append(target)
            elif matcher is not None:
                target["matcher"] = matcher
            target["hooks"] = managed_entries + target.get("hooks", [])
        
        hooks[event] = [g for g in groups if g.get("hooks")]
    return settings







SEED_TOKEN = "__" + "HOME" + "__"


def _store_root():
    return os.environ.get("AGENT_CONTEXT_STORE") or os.path.join(HOME, ".agent-context")


def _substitute_home(obj):
    if isinstance(obj, str):
        return obj.replace(SEED_TOKEN, HOME)
    if isinstance(obj, dict):
        return {k: _substitute_home(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_home(v) for v in obj]
    return obj


def seed_settings():
    "The store's settings seed with this HOME substituted, or None when there is none.\n    A seed that exists but does not parse raises: a fresh machine must not silently start\n    with no settings."
    path = os.path.join(_store_root(), "global", "settings-seed.json")
    try:
        with open(path) as f:
            return _substitute_home(json.load(f))
    except FileNotFoundError:
        return None


def write_json(path, doc):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def main():
    args = sys.argv[1:]
    
    
    if any(a in ("-h", "--help") for a in args):
        print(__doc__)
        return 0
    
    
    rest = list(args)
    if "--dispatch" in rest:
        at = rest.index("--dispatch")
        del rest[at:at + 2]
    unknown = [a for a in rest if a.startswith("-")]
    if unknown:
        print("home-settings-sync: unknown flag %s" % unknown[0], file=sys.stderr)
        return 2
    dispatched = None
    if "--dispatch" in args:
        at = args.index("--dispatch")
        value = args[at + 1] if at + 1 < len(args) else ""
        del args[at:at + 2]
        dispatched = tuple(e.strip() for e in value.split(",") if e.strip())
    target = args[0] if args else hp.settings_file(HOME)
    global LIVE_CLIENT
    if os.path.realpath(target) == os.path.realpath(hp.settings_file(HOME)):
        LIVE_CLIENT = HOOK_CLIENT.installed()
    if not os.path.exists(target):
        seeded = seed_settings()
        if seeded is None:
            print("home-settings-sync: no settings.json; skipping", file=sys.stderr)
            return 0
        os.makedirs(os.path.dirname(target), exist_ok=True)
        write_json(target, seeded)
        print("home-settings-sync: seeded settings.json from the store's settings-seed.json")
    with open(target) as f:
        settings = json.load(f)
    before = json.dumps(settings, sort_keys=True)
    registry = {}
    sync(settings, dispatched, registry)
    after = json.dumps(settings, sort_keys=True)
    changed = False
    if before != after:
        write_json(target, settings)
        changed = True
    
    
    registry_path = HOOK_REGISTRY.registry_path(target)
    if (not os.environ.get("HOOK_DISPATCH_REGISTRY")
            and os.path.abspath(target) != os.path.abspath(hp.settings_file(HOME))):
        
        
        registry_path = os.path.join(os.path.dirname(os.path.abspath(target)),
                                     HOOK_REGISTRY.REGISTRY_NAME)
    wanted = {"hooks": registry} if registry else None
    if (json.dumps(HOOK_REGISTRY.load_json(registry_path), sort_keys=True)
            != json.dumps(wanted, sort_keys=True)):
        if wanted is None:
            try:
                os.remove(registry_path)
            except OSError:
                pass
        else:
            write_json(registry_path, wanted)
        changed = True
    if changed:
        print("home-settings-sync: settings.json managed hooks + settings synced")
    else:
        print("home-settings-sync: settings.json already in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
