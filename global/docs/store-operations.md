Full reference for the agent-context store. The always-loaded global instruction
keeps only the essentials and points here (`get_doc("store-operations.md")`)
for the details below.

The agent-context store is a **git repository** at `~/.agent-context` holding
everything an agent needs beyond the codebase itself, instructions, memory,
docs, skills, commands, hooks, scripts, as plain files, one per entity. It is
served by the `agent-context` MCP server: there is **no database**, the files
are the source of truth (indexed in memory, synced across machines via git).
Every agent, regardless of platform, should use it the same way, through the
MCP tools below.

## Bootstrap

On your first turn, call `get_session_context(cwd)`. It returns:
- **Project identity**, canonical remote, display name, stack, workspace.
- **Instructions**, all `always`-loaded instruction blocks (global + project).
- **Memory index**, `{legend, global{…}, project{…}}`, split by scope. Each block holds
  `rows` (one text line per loaded memory: `<type> <slug>, <description>`, type
  `f`/`p`/`r`/`u`) and `lazy` (a space-separated slug roster for memories tiered out of
  the loaded set). A lazy memory is **not hidden**, you can see it exists and read it
  with `get_memory(slug[, project])`; only its description isn't pre-paid. A block may
  also carry `over_budget` when the scope outgrew its index budget and the stalest rows
  were demoted into `lazy`; that is a signal to prune, not an error.
- **Open audit observations**, unresolved self-improvement findings.
- **Machine identity**, the physical machine this session runs on (`machine` block); see *Machine scoping*. A session that reaches the server over the network with no machine identity gets a `machine` block of nulls with `unknown: true` and a top-level `machine_warning` (install or update the relay, or use a per-machine token; a long-running desktop app relay may need restarting). It is never answered as the daemon host. The mobile connector (token id `mobile-oauth`) gets the same null block with a short warning that names no relay, since it runs none.
- **`index_health`**, present *only* when this session's scopes have an actionable
  hygiene problem (over-long descriptions, a scope over its row budget, stabilized
  worklogs still holding a loaded slot, a payload over the ceiling). Counts and slugs
  only. If you see it, fix what it names before adding to the index. A clean index
  omits the key entirely.
- **`fleet_health` / `daemon_health`**, present only when a machine's daemon needs
  attention, or when THIS machine's store is not syncing. Say it to user before
  relying on the store.
- **`active_sessions`**, present only when another live session, on any
  machine, is on this project or in the store: one line each, `machine · project ·
  worktree or cwd · seen N min ago · editing a.cs, b.cs`. Read what they edited before
  you edit it, and say so to user if you are both on one task. Claims come from the
  `record-session-claim` hook (every turn, every Edit/Write) through the daemon's
  fleet row: a change is published within seconds and bootstrap fetches before it
  reads, so a claim is at most about a minute old. The daemon knows which session is
  calling (the connection's peer process against the claim's recorded ancestry), so
  your own claim is never listed and two sessions in one directory on one machine
  are told apart. A harness that runs no hook (pi, opencode, copilot) is claimed for
  by the daemon itself on bootstrap and on each write. Every write tool's receipt
  likewise carries `warning` when another session, on this machine or another, wrote
  or hand-edited the same entity in the last thirty minutes, the write went through
  (warn, then allow); read it back before writing again.

Instructions in force are **global + the project's workspace + the project**. A
workspace instruction applies to every project whose `project.toml` names that
`workspace`.

Follow the returned instructions for the rest of the session.

**Keeping the bootstrap cheap.** This payload loads in every session on every machine,
so its cost is paid thousands of times over. Two levers, in order: `delete_entity("memory", slug)` for a
`project` worklog once its work has stabilized and holds no durable lesson, and
`upsert_memory(slug, load_behavior="lazy")` for a memory that is narrow enough that anyone
in that territory would find it by search. `check_integrity()` reports
`bootstrap_footprint` per scope plus `over_budget_scopes`; `upsert_memory` warns on the
write that pushes a scope past its budget.

## Entity types and how to use them

How to write the text itself: [[authoring.md]].

> **The store is not the only copy, and it does not always win.** Entities are PROJECTED
> onto disk, into `.agents/` and then `.claude/`, and it is the disk copy the harness
> actually serves. So a stale file can shadow a store entity indefinitely: the store reads
> correct, `get_entity` returns the right body, and the session runs the old one. Worse, a
> harness directory anywhere but the repo root is in no store scope at all, so
> `list_entities` cannot see it and no audit finds it. Read
> `get_doc("agents-layout.md")` for the projection map, and treat "I fixed the store
> entity" as half a fix until you have checked what is on disk.

| Entity | What it stores | How to consume | How to write |
|--------|---------------|----------------|--------------|
| **Instructions** | Behavioral rules agents must follow. Loaded automatically by `get_session_context` when `load_behavior: always`. | Read and obey, these are your standing orders. `get_instructions(project?)` to reload. | `upsert_instruction(title, body, load_behavior?, project?)` |
| **Memory** | Persistent cross-session knowledge, user preferences, project decisions, external references, feedback. Types: `feedback`, `project`, `reference`, `user`. | `list_entities("memory", project?)` for the index, `get_memory(slug, project?)` for full content, `search_all(query, project?, kind="memory")` for full-text search. Access when context seems relevant or the user references prior work. | `upsert_memory(slug, type, description, body, project?)`. Always `search_memories` first to avoid duplicates. |
| **Docs** | Reference documentation, feature catalogs, architecture guides, API docs, workflow runbooks. Hierarchically organized by path (e.g. `features/drive-mode.md`). Lazy-loaded (not auto-included in session context). | `list_entities("doc", project?, path_prefix?)` to browse, `get_doc(path, project?)` to read, `search_all(query, project?, kind="doc")` to search. Load when working in a relevant area. | `upsert_doc(path, body, title?, project?)` |
| **Skills** | Reusable multi-step procedures an agent can execute, build scripts, deployment workflows, audit routines. Each has a name, description, and a body containing step-by-step instructions. | When the user asks to run a skill (by name or by describing what they want), look it up: `get_entity("skill", name, project?)`. Read the body and **follow the procedure it describes**, the body is your instruction set, not a passive document. `list_entities("skill", project?)` to discover available skills. Some agents have built-in skill invocation (e.g. Claude Code's `/skill` command), if yours does, use it; otherwise, fetch and follow manually. | `upsert_skill(name, description, body, project?)` |
| **Commands** | Named procedures similar to skills, typically invoked as slash commands. Each has a name, description, and body with instructions to follow. | `get_entity("command", name, project?)` to fetch, then follow the body. `list_entities("command", project?)` to discover. If your agent supports slash commands natively, these map to that mechanism. Otherwise, fetch and follow manually. | `upsert_command(name, body, description?, project?)` |
| **Scripts** | Executable shell/python scripts stored in the agent-context store, worktree finish scripts, sweep scripts, maintenance routines. Each has a name, language (`sh`/`python`), and `script_body`. | `get_entity("script", name, project?)` to fetch. To run: write `script_body` to a temp file, execute it with the appropriate interpreter (`bash`, `python3`), then clean up the temp file. Or pipe directly: `echo "$body" | bash`. | `upsert_script(name, script_body, language?, description?, project?)` |
| **Hooks** | Event-driven scripts that run automatically in agents that support hook infrastructure. Each has an `event_type` (`PreToolUse`, `PostToolUse`, `SessionStart`, `Stop`, etc.), an optional `matcher`, and a `script_body`. | Hooks are **informational for most agents**, they describe automation that the user's primary agent harness enforces. Read them to understand what guardrails exist (e.g. `block-git-stash` prevents `git stash`). If your agent supports hooks, wire them up; if not, respect the *intent* of each hook manually (e.g. if a PreToolUse hook blocks a command, don't run that command). `list_entities("hook", project?)`, `get_entity("hook", name, project?)`. | `upsert_hook(name, event_type, script_body?, matcher?, language?, description?, project?)`, omit `script_body` on an existing hook to change only its metadata |
| **Audit observations** | Self-improvement findings, rule inefficiencies, stale memories, workflow gaps discovered during work. | `list_audit_observations(project?, status?, severity?, since_days?, limit?, compact?)` to review, returns `{observations, total, shown, hint}`; `since_days` answers "what happened this week", `limit` cuts after the worst-first sort, `compact=True` returns summary rows (an unfiltered call over ~200 KB comes back compact with `hint` set). Blocker/high rows load automatically in `get_session_context`. | `add_audit_observation(observation, scope, project?, evidence?)`. Resolve with `resolve_audit_observation(id, status?, resolution_note?)`. |
| **Projects** | Project identity records, canonical remote, display name, stack, local path. | `resolve_project(cwd)` or included in `get_session_context`. `list_entities("project", workspace?)` to see all. | `upsert_project(canonical_remote, display_name, ...)` |

## Quick reference

| Action | Tool |
|--------|------|
| Bootstrap session | `get_session_context(cwd)` |
| Read memory | `get_memory(slug, project?)` |
| Search memories | `search_all(query, project?, kind="memory")` |
| Save memory | `upsert_memory(slug, type, desc, body, project?)` |
| Browse docs | `list_entities("doc", project?, path_prefix?)` |
| Read doc | `get_doc(path, project?)` |
| Search docs | `search_all(query, project?, kind="doc")` |
| Run a skill | `get_entity("skill", name, project?)` → follow its body |
| Run a command | `get_entity("command", name, project?)` → follow its body |
| Run a script | `get_entity("script", name, project?)` → execute its script_body |
| Check hooks | `list_entities("hook", project?)`, `get_entity("hook", name, project?)` |
| List / delete / patch any kind | `list_entities(kind, …)`, `delete_entity(kind, key, …)`, `edit_body(kind, key, old, new, …)` |
| Log audit finding | `add_audit_observation(observation, scope, project?)` |
| Search everything | `search_all(query)` |
| Walk links from an entity | `explore(kind, key, depth?, budget_bytes?)` |
| List machines | `list_machines()` |
| Name this machine | `set_machine(display_name=name)` |
| Register a checkout path | `register_path(cwd, project?)` |

## Graph

- **Edges.** A `[[wikilink]]` outside code spans and fences (`#section` and `|alias`
  dropped), or a pointer call `get_doc("<path>")`, `get_doc("store-operations.md")`,
  `get_entity("<kind>", "<key>")` outside fences (inline code counts, because that is
  how pointers are written), is a `mentions` edge. Placeholder targets, prose in
  brackets, self-links and dangling targets make none.
- **Resolution.** From the source's scope: project, its workspace, global. A pointer's
  project argument (`get_doc("<path>", "<project>")`) starts in that project instead. A
  global source falls back to a unique match in any scope; two or more matches draw no
  edge and appear in `check_integrity` as `ambiguous_links`.
- **Cards.** `get_memory`, `get_doc` and `get_entity` for memories, docs, skills and
  commands return `links`: `→ mentions memory <key> (9.6 KB): <description>` for an
  out-link, `←` for a backlink. At most 12: out-links by their backlink count, then
  backlinks by reads, then `+N more: explore("<kind>", "<key>")`. Instructions are nodes
  without cards, so the bootstrap is unchanged; scripts are targets only.
- **`explore(kind, key, depth=1, rel=None, budget_bytes=4000, project=None,
  workspace=None)`.** Cards only, breadth-first, cut before the byte budget with
  `truncated: N`. Depth is 1 or 2. A card below depth 1 starts with the key it was
  reached from. A body over 64 KB is carded but not walked unless it is the start.
- **Freshness.** Every index change (a write, a delete, `sweep_vanished`, a hand edit
  seen by `_fresh`, a sync reload) bumps `graph_generation`; the next read re-resolves,
  re-parsing only bodies that changed.
- Cards and `explore` do not count as reads in usage.

### Typed links

Code: the typed half of `server/src/agent_context/graph.py`, written on upsert.

### Coverage and scope navigation

`check_integrity().graph_coverage` reports nodes, edges, connected components, isolated active and archived entities, semantic isolation, zero-inbound active entities, counts by kind and scope, and missing scope roots. These structural numbers are available immediately. The separate `orphans` finding still waits for 30 days of read data before suggesting that an unread entity may be pruned.

Every active entity gets a derived `in_scope` edge to its scope root doc: `agent-context-store.md` at global scope and `<scope-name>.md` in a project or workspace. Project roots connect to their workspace root when one exists, otherwise to the global root; workspace roots connect to the global root. Archived docs, identified by an `archive` or `*-archive` path segment, receive no scope edge. None of these edges rewrites entity files or claims that two bodies discuss the same subject. Cards put `in_scope` after content links. `explore(..., rel="in_scope")` shows just this hierarchy.

Hooks and scripts are graph nodes. Their executable bodies are not parsed as prose, and their `get_entity` results still carry no cards. `set_entity_links` can add explicit typed relations to scripts and hooks without changing code bytes; `explore` shows their links. A hook target requires `hook:<name>`, and `script:<name>` selects a script when names collide. Bare hook names remain integrity claims. An unqualified script and hook sharing a name draw no edge and produce an ambiguity warning. `semantic_isolated_active` counts active entities with no subject link beyond scope navigation. `semantic_isolated_prose` and `semantic_isolated_executable` divide that count; `semantic_isolated_prose_items` lists the prose nodes to review. These are coverage signals, not evidence that a standalone entity should be forced into a subject relationship.

Doc bodies also contribute `mentions` edges from local Markdown links to `.md` files, resolving paths relative to the source doc and preserving section anchors. Images, code spans and fences, external URLs, and URI schemes do not create edges. This indexes existing catalogs such as `features/README.md` without rewriting their links.

### Sections

Code: `server/src/agent_context/sections.py`, cached in `graph.py`.

## Scope resolution

Most entities support an optional `project` parameter. When provided, the store
returns the project-scoped version; when omitted, it returns global. Some
lookups (skills, commands, scripts, hooks) fall back to global scope if nothing
is found at project scope.

## Machine scoping (multi-machine context)

The store is shared across machines **via git**, each machine clones the
`agent-context` repo; the daemon commits and pushes your writes and pulls peers'
changes on start, so the file tree converges everywhere. `get_session_context`
returns the current machine as the `machine` block (a network caller with no identity gets
`unknown: true` and no machine row is written for it);
`list_machines()` shows all known machines with the current one flagged, and none flagged
for an unknown caller.

## Workspace-scoped skills and commands

Two things to know:

- **There is no MCP write path for workspace scope.** `upsert_skill`/`upsert_command`
  take `project` only, so a workspace entity is placed by moving its directory in the
  store (`git mv global/skills/<name> workspaces/<ws>/skills/<name>`, the `uuid:`
  frontmatter carries over unchanged) and read back normally afterwards.
- **`project-materialize.py` stages workspace skills/commands FIRST**, then the
  project's own on top, so a project-scoped entity of the same name wins. This is what
  puts the file on disk in each member repo; the harness reads skills from files, and
  `global/` would publish them to every repo on the machine.

## Viewing in Obsidian

The store root is an Obsidian vault, and a read-only viewer (policy): open `~/.agent-context` in Obsidian to browse and graph the store, and make every change through MCP. `.obsidian/app.json` sets `defaultViewMode` to `preview`, so every note opens in Reading view.
