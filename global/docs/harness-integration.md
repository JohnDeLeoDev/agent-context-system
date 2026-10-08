# Harness integration — one store, every coding harness

How every coding harness on a machine is wired to the single source of truth
(`~/.agent-context`): the same instructions, MCP servers, skills, and guard
hooks, materialized from canonical manifests so nothing drifts per-harness.

Read this when adding a harness, adding an MCP server, changing which hooks are
enforced cross-harness, or debugging why a harness can't see the store.

## The two canonical manifests

Read or change MCP entries through `run_store_task("mcp-manifest", args=["read"])`,
`args=["upsert", "NAME"]` with one JSON server specification in `stdin`, or
`args=["remove", "NAME"]`. Add `--dry-run` to validate without writing.
Writes preserve other entries and reject credentials, unsupported fields and unsafe scopes.
The normal materializer publishes the resulting manifest to each harness.

Both live in `global/`, are **secret-free**, git-synced, and `{HOME}`-portable:

## The materializer

**`global/scripts/harness-materialize.py`** renders both manifests into each
installed harness's native config, non-destructively (preserves unmanaged
keys/servers/hooks), idempotently. It also:
- symlinks each harness's global instruction file → store `AGENTS.md`
- builds `shared-skills/` (a git-ignored per-machine projection) from the store's
  `global/skills`, frontmatter stripped through `frontmatter_strip.py`, and points every
  harness's skills dir at it. Claude Code reads `~/.claude/skills`, which `home-materialize.py` fills; the other harnesses read `shared-skills/`. An empty store skills dir
  records a finding and leaves the mirror alone.
- **prunes dead `~/.orca` hook wiring** wherever it lingers (orca is retired)

It also projects the store's **global commands** into `~/.config/opencode/commands`
(`materialize_commands`), because opencode is the one harness that reads
Claude-compat *skill* dirs but has no compat path for *commands* — see the
Commands section below.

It runs from **`home-materialize.py`** at every Claude SessionStart (so one
Claude session heals every harness), and stands alone: `python3
global/scripts/harness-materialize.py`. A fresh machine gets it transitively —
`setup.sh` calls `home-materialize.py`.

## Tool and question capabilities

Use the capability actually exposed by the active harness. The universal instruction
names the job, not a tool name that only one harness implements.

| Job | Claude Code | Codex desktop |
|---|---|---|
| Symbol navigation | Project LSP MCP | Project LSP MCP if exposed; if absent, use the available source-search fallback |
| Read a source file | `Read` with a bounded offset and limit | Available file reader; otherwise `exec_command` with a bounded read |
| Edit a source file | `Edit` or `Write` | `apply_patch` |
| Find files or literal text | Absolute-path `find` or `grep -rn` | `rg --files` or `rg`, scoped to an absolute path |
| Structured choice | `AskUserQuestion` | `request_user_input_async`; keep the turn open until answers arrive |

opencode 2.x has a `question` tool with the same fields (`question`, `header`, `options`, `multiple`); an approval question goes in a call of its own.

These are observed mappings, not a promise that every session exposes every tool.
Inspect the current tool roster before falling back to shell. In Codex Default mode,
`request_user_input` may reject calls; `request_user_input_async` returns before the
answer and its prompt disappears if the turn ends. A missing LSP tool permits a source
search fallback; a configured LSP that is down is a stop, not negative symbol evidence.

Claude Code's managed `WorktreeCreate` hook places new worktrees under
`.agents/worktrees/`. The Codex hook projector reports `WorktreeCreate` unsupported, so
Codex has no native worktree-creation event to hook at all -- `git worktree add` through
the shell is its only way to make one. That shell door is
enforced: `require-worktree-add-location` is a MANAGED
PreToolUse(Bash) entry in `home-settings-sync.py`, which `harness-materialize.py`'s
`render_codex_hooks()` already translates into Codex's own `hooks.json` the same way it
carries every other MANAGED Bash guard there. It refuses a `git worktree add` whose
target does not resolve under `.agents/worktrees/<name>` and names the corrected
command; a worktree already under `.claude/worktrees/` stays valid until landed, since
the hook only judges a new `git worktree add`, never an existing worktree. Battery:
`test-require-worktree-add-location.py`.

## Per-harness result

Codex content projection runs from `home-materialize.py` after the Claude home projection, and from `agents-materialize.py` for project-scoped content. It owns only generated files and managed MCP blocks, preserving foreign configuration. The exclusive `xcode` MCP bridge remains Claude-only because a second client evicts Claude's live connection. Codex project hooks and project config require Codex's own project trust; materialization does not bypass that consent. A large project skill set may exceed Codex's initial skill-list budget even though the files remain discoverable. Supporting scripts and docs continue to be projected under `.claude/` for procedures that reference those paths. On a fresh Codex session, restart if newly projected skills do not appear.

## pi hooks and commands

`harness-materialize.py` renders `~/.pi/agent/extensions/agent-context-hooks.ts` from the
`dispatch-*` entries in `global/hooks-manifest.json` (`render_pi_hooks`,
`materialize_pi_hooks`). The extension runs the Claude hook dispatcher once per event,
with a Claude-shaped payload on stdin, the same way Claude does:

| pi event | Claude event |
|---|---|
| `session_start` | `SessionStart` |
| `input` | `UserPromptSubmit` |
| `tool_call`, `user_bash` | `PreToolUse` |
| `tool_result` | `PostToolUse`, `PostToolUseFailure` |
| `agent_end` | `Stop` (a refusal becomes a new turn; the third in a row ends the turn) |
| `session_shutdown` | `SessionEnd` |

A refusal is exit 2, a `permissionDecision` of deny, or a `decision` of block; it blocks the
call. Every other dispatcher outcome (crash, timeout, missing script) allows it, the same
fail-open the dispatcher documents. Slash commands register from `global/commands`.

Known gaps: `PreCompact` and `SubagentStop` are not wired (pi has no matching event or Claude wires
them directly); pi's `agent-context` MCP tools reach the dispatcher under their bare names, so a
guard matcher written as `mcp__agent-context__*` does not fire; and pi-code stays enabled, so on
a machine that has both, each event runs twice until pi-code's hook bridge is switched off.
The battery is `test-pi-hooks-extension.py`.

## Hooks, scripts and docs run from the store

No harness reads `~/.claude/{hooks,scripts,docs}`, and `home-materialize.py` does not
copy hooks or scripts anywhere. `harness_paths.hooks_dir()` and `scripts_dir()`
return the store's `global/hooks` and `global/scripts`, and every wiring names those paths:
Claude's and Xcode's `settings.json`, the pi extension, `~/.codex/hooks.json`, opencode's
generated plugin, the chezmoi wrappers and the systemd unit. The store files are mode
0644, so a caller runs them through an interpreter and never checks an exec bit.

- **Docs** are not projected. They are read over MCP with `get_doc`; a command body
  hands the agent a `get_doc` pointer. `home-materialize.retire_shared_docs()` removes
  the old `~/.agent-context/shared-docs/` copy.
- **The dispatcher registry** is `~/.agent-context/hook-dispatch.json` (git-ignored, written
  by `home-settings-sync.py`). `$HOOK_DISPATCH_REGISTRY` overrides it for both the dispatcher and
  `hook-registry.registry_path()`, which is how a test syncs a fixture settings file without
  touching the live registry. A test that syncs with the real HOME and no override deletes the
  live registry, and every guard then fails open until the next session start rewrites it.
- **Retirement** is `home-materialize.retire_claude_projections()`. It removes the old
  `~/.claude/{hooks,scripts,docs}` and `~/.claude/hook-dispatch.json` only when no harness
  config (Claude and Xcode `settings.json`, `~/.codex/hooks.json`, the pi extension) still names
  them, and it runs before the settings sync so the run that rewrites the wiring never deletes
  what a live session is still calling.
- The invariant `no-claude-projection-paths` fails on a store hook or script that names a retired
  directory. Battery: `test-harness-neutral-4-5.py`.

### User-scope CLAUDE.md is generated from AGENTS.md

Claude Code reads `~/.claude/CLAUDE.md` at user scope and does not read `AGENTS.md` there.
`home-materialize.project_claude_md()` therefore generates `~/.claude/CLAUDE.md` from the
store's `AGENTS.md` at every session start, ahead of the steady-state skip because
`AGENTS.md` sits outside the paths that skip watches. The file is an exact mirror: a `<wikis>`
block in it is dropped, because no tool generates it and its path does not exist.

## Xcode (Xcode 27's built-in agents)

**Xcode's Claude agent does not read `~/.claude`.** It runs its own bundled
claude binary (`~/Library/Developer/Xcode/CodingAssistant/Agents/claude/<ver>/`)
with `CLAUDE_CONFIG_DIR` pointed at
`~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig`. That directory is
its `~/.claude` equivalent: global `CLAUDE.md`, `settings.json` (so the git /
worktree guards live or die there), `skills/`, `commands/`, and `.claude.json`
MCP servers all come from it. Without the wiring below an Xcode agent session
runs with no store instructions, no skills and no guardrails.

`materialize_xcode()` in the materializer wires all five, plus:

Verify the wiring with Xcode's own binary:

```bash
CLAUDE_CONFIG_DIR=~/Library/Developer/Xcode/CodingAssistant/ClaudeAgentConfig \
  ~/Library/Developer/Xcode/CodingAssistant/Agents/claude/*/claude mcp list
```

### The `xcode` MCP server (the other direction)

## A stale disk copy outranks the store, silently

## Guard-hook validation status

## Inter-agent messaging

What each harness does with a peer message (policy); use and rules are in
`get_doc("inter-agent-messaging.md")`.

## How to…

- **Add an MCP server:** edit `global/mcp-servers.json` (set `harnesses`), re-run
  `harness-materialize.py` (or start a Claude session).
- **Add/retarget a cross-harness hook:** edit `global/hooks-manifest.json`, extend
  `materialize_hooks()` in `harness-materialize.py` if a new harness/event shape
  is involved.
- **Add a harness:** add an `MCP_TARGETS` entry + instruction/skill/hook wiring in
  `harness-materialize.py`, and list it in both manifests. A harness whose config
  home is not a single JSON file (Xcode) gets its own `materialize_*` function
  instead, called from `main()` after `materialize_skills`.

## Known follow-ups

- `machine-bootstrap.py` (what the `setup.sh` launcher execs) still carries two
  per-harness steps that predate this materializer: pi's skills key and Copilot
  CLI's `permissions-config.json`. Harmless, but duplicated logic; fold them into
  `harness-materialize.py` when convenient.
- `shared-skills/` and `shared-commands/` come from the store alone, so they fill on a
  machine with no Claude directory.
- Xcode guard hooks are unvalidated (above). Also unverified: whether Xcode's
  agent honors `CLAUDE_CONFIG_DIR/commands` for slash commands — it maintains its
  own `SlashCommandCache/claude-code.json`.

## Project-scoped MCP servers for opencode

opencode gets scoped servers too, but by a different route from Claude, because
its two config files have different owners:

- **Global `~/.config/opencode/opencode.json` is chezmoi's**, end to end
  (`dot_config/opencode/private_opencode.json.tmpl`). The materializer deliberately
  does not write it — two writers meant permanent chezmoi drift on a file whose
  content was already correct. A **global** server for opencode is a template edit.
- **Scoped servers go in each repo's own `opencode.json`**, written by
  `materialize_opencode_projects()`. opencode has no `projects.<path>` section in its
  global config the way Claude does; its per-directory config *is* a file in the repo.
  `.git/info/exclude` (never `.gitignore`) keeps it out of the user's tree.

### Widening a scoped server to another harness is not safe

The four LSP bridges (swift/csharp/typescript/kotlin) are safe to share and are
`["claude", "opencode"]`, because `lspd.py --mcp` joins the one lspd daemon: every client is
another socket onto one warm server per main checkout, not another language server.

## Commands

**opencode reads Claude-compat skill dirs but has no compat path for commands.**
Its six skill roots include `~/.claude/skills`, a repo's `.claude/skills` and both
`.agents` equivalents, so every store skill works in opencode with no wiring. Commands come from two places,
`~/.config/opencode/commands` and a repo's `.opencode/commands`, so store commands
need their own projection.

Both halves project, sharing one renderer and one ownership manifest:

| Half | Source | Destination | Driven by |
|---|---|---|---|
| global | store `global/commands/` | `~/.config/opencode/commands/` | `materialize_commands()` in harness-materialize.py |
| per-repo | `<repo>/.agents/claude/commands/` | `<repo>/.opencode/commands/` | `agents-materialize.py`, calling `harness-materialize.py --commands SRC DST` |

Four decisions:

- **Split across two scripts on purpose.** harness-materialize.py cannot enumerate
  this machine's checkouts — the file store resolves a project by an in-repo
  `.agents/project-id` marker, not a path registry — while agents-materialize.py is
  already handed the repo root. The renderer lives in one place and is reached from
  the other by `--commands`.
- **A copy, not a symlink.** The global destination is shared with commands opencode
  got elsewhere (the caveman plugin ships six there), and Claude frontmatter carries
  keys opencode does not define: `allowed-tools`, `disable-model-invocation`, and a
  bare `model: sonnet` where opencode wants `provider/model`. Output is rewritten
  down to opencode's documented keys instead of hoping the rest is ignored.
- **Ownership is a manifest (`.agent-context-commands.json`), not a heuristic.** A
  destination file we did not write is never overwritten and never pruned. Without the manifest the prune would have to guess, in a directory full of
  somebody else's commands.
- **A command with no frontmatter falls back to its H1.** opencode requires a
  description; Claude does not, and the store's `init-project` reaches
  `.claude/commands` with no frontmatter at all.

`.opencode/` is kept out of the repo via `.git/info/exclude`, never `.gitignore`.

Verify with `opencode serve` and `GET /command` in a project: the list holds the
global and project store commands, with foreign commands untouched.

## Node tools behind MCP servers and LSP

`xcodebuild-mcp`, `firefox-devtools`, tsgo and copilot-api come from the store's pnpm tools manifest. `npx -y <pkg>@latest` downloads unreviewed code at every session start and cannot start offline, and nothing keeps a global npm install current.

| Piece | Where |
|---|---|
| Manifest, every tool at `latest` (user: no pins) | `global/node-tools/package.json` |
| pnpm settings and the copilot-api patch | `global/node-tools/pnpm-workspace.yaml`, `global/node-tools/patches/` |
| Installer | `global/scripts/node-tools-sync.py` (battery `test-node-tools-sync.py`) |
| Installed tree, one symlinked generation | `~/.local/share/agent-context/node-tools` |
| Linked bins (`agentContextBins`) | `~/.local/bin/copilot-api`, `~/.local/bin/tsgo` |

`mcp-servers.json` launches `{HOME}/.local/share/agent-context/node-tools/node_modules/.bin/<bin>`. A sync runs from chezmoi install-packages on a new machine and on demand (policy retired the daily run); nothing runs it at session start, and `node-tools-sync.py --check` never fetches. pnpm comes from Homebrew on the Macs, and from the npm that apt or Synology Package Center provides elsewhere. The invariant `node-tools-pinned` refuses npx, global npm installs (except that pnpm bootstrap) and any version pin in the manifest.
## Tool-result compression

`compress-tool-output` shortens a large, repetitive tool result before the model reads it. The decision, the rules, what it never touches and what has been seen live are in `get_doc("agent-context.md")`, policy. How the one hook body reaches each harness:

| Harness | Route | Replacement call |
|---|---|---|
| **Claude Code**, **Xcode** | MANAGED PostToolUse entry, through `hook-dispatch.py` | `hookSpecificOutput.updatedToolOutput`, in the tool's own result shape |
| **Codex** | the same entry, through `codex-hook-adapter.py` | `decision: block`, whose reason Codex gives the model as the result |
| **pi** | `tool_result` in the generated extension, through the dispatcher | the handler returns new `content` |
| **opencode** | `execute.after` in the generated plugin, which runs the hook itself | `event.result.content` and `output` are replaced |
| **Copilot CLI** | a PostToolUse entry in `~/.copilot/settings.json` that runs the hook itself | top-level `modifiedResult` |
| **antigravity**, **Claude Desktop** | none | no hook can see or replace a tool result |

A new harness adapter sends the result as `tool_response: {"stdout": text}` and reads `updatedToolOutput.stdout` back.
