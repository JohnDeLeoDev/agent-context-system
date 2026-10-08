# lspd: servers, diagnosis, behavior

Detail for memory `lsp-one-daemon-per-workspace`. Architecture and research: `get_doc("code-intelligence-architecture.md")`.

## Servers

TypeScript uses tsgo: `typescript-language-server` returned 1 reference where tsgo returned 8 across 3 files. tsgo installs from the pnpm node-tools root (`node-tools-sync.py`, package `@typescript/native-preview`), so no project's own `typescript` dependency changes.

## Built-in LSP tool stays off

Claude Code's built-in `LSP` tool spawns one server per session, never reused or cleaned up, and ignores `transport: socket`. Disable it per project. A project-scoped `enabledPlugins` entry overrides the global one, so check the project's own `.agents/claude/settings*.json`.

## Workspace binding

The workspace is the main checkout, resolved through `git --git-common-dir`. A worktree path is a different workspace to every server: a cold index, and for Kotlin a corrupted shared cache (Kotlin/kotlin-lsp#178). A symbol that exists only in unlanded worktree code is not in the index; the file being edited still resolves because the bridge sends `didOpen`. Override per invocation with `AGENT_LSP_WORKSPACE`.

## Diagnosis

- `python3 ~/.agent-context/global/scripts/lspd.py --status`: live daemons and whether each answers. `canary=ok`: a known question got its answer. `canary=EMPTY`: the server is up and returns nothing; its answers are not evidence. `canary=unknown`: the daemon predates the check and gets it at its next start. `canary=no-symbols`: no reachable document had a symbol to probe. `--status --key K` scopes rows and exit code to one key.
- `code=STALE`: the daemon runs an older build of lspd.py than the one on disk. `--restart` restarts only the language server; the daemon keeps its build. Retire it with `lspd.py --upgrade --key <key>` (`--force` with sessions attached); the next attach respawns it. It also adopts the new build at its next natural restart.
- Logs: `~/.local/state/agent-context/lsp/logs/<key>-<hash>.log` (daemon), `.server.log` (server stderr). `LSPD_TRACE=1` writes routed traffic to `.trace.log`.
- `LSPD_SEED="*.py:python"` gives any key a seed spec, for tests and probes.
- Regression battery: `python3 ~/.agent-context/global/scripts/test-lspd.py`. Run it after any change to lspd.py.
- Never run `roslyn-language-server` directly to diagnose C#. Outside `lspd.py --mcp` it inherits Homebrew's `DOTNET_ROOT`, which has no `shared/` tree, and exits 131 with "You must install .NET to run this application".

## Canary details

The canary tries every open document, then opens up to `LSPD_CANARY_CANDIDATES` more seed files. basedpyright answers `references` with null on `def` and a list on the name, and sends flat symbols whose range starts at `def` or the first decorator line: the canary finds the name up to 12 lines down and counts null as zero during calibration. The probe is placed from the text the server was handed (didOpen, whole-document didChange, re-seat, replay; disk only when unknown), in UTF-16 columns, never inside a decorator argument that spells the name. A didOpen is forwarded whenever the server does not hold the document.

## What lspd does for each server

Rewrites `initialize.processId` to its own pid (else the server exits with its first client); strips null params (tsgo rejects them); answers `workspace/configuration` itself (a null answer aborts Roslyn, exit 134); sends Roslyn's `solution/open`; drops `roslyn-source-generated:` URIs; seeds the project, since tsgo and Roslyn load lazily and answer "not found" until a file opens; holds navigation requests until the index is warm; restarts a crashed server in place, since Claude Code does not respawn a failed MCP server.

## Manual step

## Enforcement (never-proceed-without-lsp)

Enforced by two global hooks: `lsp-failure-tripwire` arms a per-session flag on a dead-server
signature and clears it on any successful call; `require-working-lsp` denies edits, Task, Agent
and Workflow while that flag is set (Bash, the LSP tools and `*.md` / `.agents/` / `.claude/`
writes stay open, so diagnosis and retesting are possible). Their exact signatures, matchers and
escape hatches live in the hook entities and their tests: the `lsp-failure-tripwire.py` cases in
`hook-test-cases.py` (run with `python3 ~/.agent-context/global/scripts/hook-test-run.py --hook
lsp-failure-tripwire`) and `test-lspd`. Both are registered in `home-settings-sync.py`'s MANAGED
table: `upsert_hook` alone leaves a hook wired to no lifecycle event, so it never runs.

Repair from inside a session (a server answering wrongly, a server that will not start, a
bridge that never connected, and why nothing is killed by hand):
`get_doc("core-system-health.md", section="In-session repair: replacing a language server")`.

## Rust symbol search and configuration changes

Repair a degraded server without asking (memory `always-repair-degraded-core-systems`): use the supported tooling, leave unrelated sessions alone, and verify the result.

rust-analyzer's default workspace symbol search is `only_types`, under which type lookup and hover succeed while function names return not found. lspd requests `workspace.symbol.search.kind=all_symbols`. A server-child restart keeps the daemon's old configuration: retire the daemon with `--upgrade --key rust-analyzer` once no clients are attached (`--force` only for diagnostic clients you own), then attach again.

The test runner's private home sits under a short `/tmp` root, because Darwin limits a socket path to 103 bytes.
