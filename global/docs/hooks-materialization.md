# Workflow guardrail hooks: rules, wiring and materialization

How the store's hooks reach each harness, and how their wiring repairs itself every
session. What each hook enforces lives in its own entity description,
`list_entities("hook")`, and is not listed again here.

## Why hooks, not prose

Prose alone did not hold the worktree, deploy and push rules: an agent that misses
the text breaks them for a whole session. Those rules are hooks. A refused tool call
means the guard did its job.

## Claude Code: store to disk

Hook scripts live in the store under `global/hooks/` and run in place, wired at their
store path. `home-materialize.py`, the first SessionStart hook in `~/.claude/settings.json`,
projects skills, commands and agents into `~/.claude` every session, so a machine
cannot drift. Docs are not projected: they are read over MCP. It does not copy hooks or scripts. It
retires the old `~/.claude/{hooks,scripts,docs}` copies once no harness config names
them, and never in the run that rewrites the wiring.

Drift guard: a projection file that is newer than its store source and
differs from it (a hand edit of the copy) is reported on stderr and push-notified, at
most once a day, before the store overwrites it.

`home-materialize.py` also runs `~/.local/bin/git-mirror-converge --quick` at every
SessionStart.

## Claude Code: settings.json wiring

`home-materialize.py` ends by running `home-settings-sync.py`, which merges the store's
managed hook set into `~/.claude/settings.json` without touching machine-local entries.
The `MANAGED` table in that script is the only list of what is wired and in what order.
Do not copy it into a doc. The merge is safe to re-run on any machine. It also prunes
entries that point at a missing file under `~/.agent-context/global/hooks/`, so retiring a hook on
one machine retires it everywhere.

### One dispatcher per event

Claude Code starts one process per matching hook, which is slow when many hooks match
one call. For each event in `DISPATCHED_EVENTS` in `home-settings-sync.py` (every
event with more than one hook: `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `UserPromptSubmit`, `Stop`, `SessionStart`, `SessionEnd`; the
single-hook events Notification, SubagentStop and PreCompact stay direct by user's
choice), settings.json holds one command,
`~/.agent-context/global/scripts/hook-dispatch.py <Event>`, and the event's guards are written to
`~/.agent-context/hook-dispatch.json`. The dispatcher reads that registry and runs the guards
in order inside one process. Two kinds of hook stay direct settings entries
(`stays_direct()`): async hooks, whose output reaches Claude on a later turn, and
`home-materialize.py`, which writes the projection the dispatcher runs from.

What this changes for anyone reading or debugging the wiring:

## Editing rules

- Edit a hook in the store (`upsert_hook` or `edit_body`), then re-materialize. One
  copy serves every harness.
- Never hand-edit `~/.agent-context/global/hooks/*`, `~/.agent-context/hook-dispatch.json` or the managed
  blocks of `~/.claude/settings.json`. All three are rewritten at every session start.
- To change which hooks are wired, or which events go through the dispatcher, edit
  `MANAGED` or `DISPATCHED_EVENTS` in `home-settings-sync.py`.

## chezmoi exemption

## pi

A generated extension runs the Claude hook dispatcher once per pi event, so there is
no pi-specific copy of any guard. Event map and known gaps:
`get_doc("harness-integration.md", section="pi hooks and commands")`.

## Other harnesses (opencode, Copilot CLI, antigravity, Claude Desktop)

`global/scripts/harness-materialize.py`, run from `home-materialize.py`, projects MCP
servers, instructions, skills and guard hooks into every installed harness from
`global/mcp-servers.json` and `global/hooks-manifest.json`, and prunes retired
`~/.orca` hook wiring. Per-harness table: `get_doc("harness-integration.md")`.
